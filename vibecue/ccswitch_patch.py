"""Conservative source adaptation for CC Switch's numeric quota export.

This module never touches a running app or user configuration. It changes only
the source integration seams, after validating every interface the bridge uses.
The updater still has to compile, test, sign and validate its resulting app.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

ADAPTER_VERSION = 1
MAX_SOURCE_BYTES = 4 * 1024 * 1024
COMMAND = 'src-tauri/src/commands/codex_oauth.rs'
COMMANDS = 'src-tauri/src/commands/mod.rs'
SERVICES = 'src-tauri/src/services/mod.rs'
LIB = 'src-tauri/src/lib.rs'
MANAGER = 'src-tauri/src/proxy/providers/codex_oauth_auth.rs'
ACCOUNTS = 'src-tauri/src/proxy/providers/copilot_auth.rs'
QUOTA = 'src-tauri/src/services/subscription.rs'
CONFIG = 'src-tauri/src/config.rs'
MODULE = 'src-tauri/src/services/vibecue_quota.rs'
INPUTS = (COMMAND, COMMANDS, SERVICES, LIB, MANAGER, ACCOUNTS, QUOTA, CONFIG)
_RAW_STRING = re.compile(r'(?:br|cr|r)(#{0,255})"')
_CHARACTER = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\n])'")
MESSAGES = {
    'source_unavailable': 'CC Switch 源码不可读取，暂不修改。',
    'source_changed': '构建期间 CC Switch 源码发生变化，已停止适配。',
    'interface_changed': '新版 CC Switch 的额度接口发生变化，需要更新适配器。',
    'setup_changed': '新版 CC Switch 的启动结构发生变化，暂不自动插入共享模块。',
    'module_conflict': '源码中已有不同的额度共享模块，暂不覆盖。',
    'template_unavailable': 'VibeCue 额度共享模块模板不可用。',
    'write_failed': '额度共享源码适配未能保存，原始内容已恢复。',
}


class PatchError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


@dataclass(frozen=True)
class FileChange:
    path: str
    before: bytes | None
    after: bytes


@dataclass(frozen=True)
class PatchPlan:
    source_dir: Path
    originals: tuple[tuple[str, bytes | None], ...]
    changes: tuple[FileChange, ...]
    module_sha256: str

    def manifest(self):
        return {'adapter_version': ADAPTER_VERSION, 'module_sha256': self.module_sha256,
                'already_patched': not self.changes,
                'changed_files': [change.path for change in self.changes],
                'source_hashes': {path: _digest(data) if data is not None else None
                                  for path, data in self.originals}}


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _read(root, relative, optional=False):
    path = root / relative
    try:
        if any(parent.is_symlink() for parent in path.parents if parent != root.parent):
            raise PatchError('source_unavailable')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise PatchError('source_unavailable')
            value = stream.read(MAX_SOURCE_BYTES + 1)
            if len(value) > MAX_SOURCE_BYTES:
                raise PatchError('source_unavailable')
            value.decode('utf-8')
            return value
    except FileNotFoundError:
        if optional:
            return None
        raise PatchError('source_unavailable') from None
    except (OSError, UnicodeError):
        raise PatchError('source_unavailable') from None


def _code(text):
    """Mask comments and Rust strings while preserving offsets/newlines."""
    result = list(text)
    i, size = 0, len(text)
    while i < size:
        start = i
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = size if end == -1 else end
        elif text.startswith('/*', i):
            depth = 1; i += 2
            while i < size and depth:
                if text.startswith('/*', i):depth += 1; i += 2
                elif text.startswith('*/', i):depth -= 1; i += 2
                else:i += 1
            if depth:raise PatchError('interface_changed')
        else:
            raw = _RAW_STRING.match(text, i)
            if raw:
                terminator = '"' + raw.group(1)
                end = text.find(terminator, raw.end())
                if end < 0:raise PatchError('interface_changed')
                i = end + len(terminator)
            elif text[i] == '"':
                i += 1
                while i < size:
                    if text[i] == '\\':i += 2
                    elif text[i] == '"':i += 1;break
                    else:i += 1
                else:raise PatchError('interface_changed')
            elif text[i] == "'" and (character := _CHARACTER.match(text, i)):
                # Lifetimes such as &'a str are not character literals.
                i = character.end()
            else:
                i += 1;continue
        for index in range(start, min(i, size)):
            if result[index] not in ('\n', '\r'):result[index] = ' '
    return ''.join(result)


def _one(pattern, code, reason='interface_changed'):
    matches = list(re.finditer(pattern, code, re.MULTILINE))
    if len(matches) != 1:raise PatchError(reason)
    return matches[0]


def _body(code, match, reason='interface_changed'):
    opening = code.find('{', match.start(), match.end())
    if opening < 0:raise PatchError(reason)
    depth = 1; index = opening + 1
    while index < len(code) and depth:
        if code[index] == '{':depth += 1
        elif code[index] == '}':depth -= 1
        index += 1
    if depth:raise PatchError(reason)
    return code[opening + 1:index - 1]


def _fields(code, kind, name, fields):
    match = _one(r'\bpub\s+' + kind + r'\s+' + name + r'\s*\{', code)
    body = _body(code, match)
    for field, rust_type in fields.items():
        if not re.search(r'\bpub\s+' + field + r'\s*:\s*' + rust_type + r'\s*[,}]', body + '}', re.MULTILINE):
            raise PatchError('interface_changed')


def _validate_interfaces(codes):
    manager = codes[MANAGER]
    _one(r'\bpub\s+struct\s+CodexOAuthManager\s*\{', manager)
    implementation = _one(r'\bimpl\s+CodexOAuthManager\s*\{', manager)
    methods = _body(manager, implementation)
    _one(r'\bpub\s+async\s+fn\s+list_accounts\s*\(\s*&\s*self\s*\)\s*->\s*Vec\s*<\s*GitHubAccount\s*>\s*\{', methods)
    _one(r'\bpub\s+async\s+fn\s+chatgpt_account_id_for_account\s*\(\s*&\s*self\s*,\s*\w+\s*:\s*&\s*str\s*,?\s*\)\s*->\s*Result\s*<\s*String\s*,\s*CodexOAuthError\s*>\s*\{', methods)
    _fields(codes[ACCOUNTS], 'struct', 'GitHubAccount', {'id': 'String', 'login': 'String'})
    _fields(codes[QUOTA], 'struct', 'SubscriptionQuota', {
        'credential_status': 'CredentialStatus', 'success': 'bool',
        'tiers': r'Vec\s*<\s*QuotaTier\s*>', 'queried_at': r'Option\s*<\s*i64\s*>'})
    _fields(codes[QUOTA], 'struct', 'QuotaTier', {
        'name': 'String', 'utilization': 'f64', 'resets_at': r'Option\s*<\s*String\s*>'})
    credential = _one(r'\bpub\s+enum\s+CredentialStatus\s*\{', codes[QUOTA])
    variants = _body(codes[QUOTA], credential)
    for name in ('Expired', 'NotFound', 'ParseError'):
        if not re.search(r'\b' + name + r'\s*[,}]', variants + '}'):
            raise PatchError('interface_changed')
    _one(r'\bpub\s+fn\s+get_app_config_dir\s*\(\s*\)\s*->\s*PathBuf\s*\{', codes[CONFIG])
    _one(r'\bmod\s+codex_oauth\s*;', codes[COMMANDS])
    _one(r'\bpub\s+use\s+codex_oauth\s*::\s*\*\s*;', codes[COMMANDS])
    _one(r'\bpub\s+struct\s+CodexOAuthState\s*\(\s*pub\s+Arc\s*<\s*CodexOAuthManager\s*>\s*\)\s*;', codes[COMMAND])
    _one(r'\bmod\s+services\s*;', codes[LIB])


def prepare_patch(source_dir, module_template=None):
    """Read and validate every seam without modifying any source file."""
    root = Path(source_dir).resolve()
    originals = {name: _read(root, name) for name in INPUTS}
    originals[MODULE] = _read(root, MODULE, optional=True)
    if module_template is None:
        module_template = Path(__file__).resolve().parent.parent / 'integrations/ccswitch/vibecue_quota.rs'
    try:
        template = module_template if isinstance(module_template, bytes) else Path(module_template).read_bytes()
        if not template or len(template) > MAX_SOURCE_BYTES:raise ValueError()
        template.decode('utf-8')
    except (OSError, ValueError, UnicodeError):
        raise PatchError('template_unavailable') from None
    if originals[MODULE] not in (None, template):raise PatchError('module_conflict')
    texts = {name: value.decode('utf-8') for name, value in originals.items() if value is not None}
    codes = {name: _code(text) for name, text in texts.items()}
    _validate_interfaces(codes)
    after = dict(originals)
    signature = _one(r'(?P<visibility>pub(?:\s*\([^)]*\))?\s+)?async\s+fn\s+query_codex_oauth_quota_for\s*\(\s*\w+\s*:\s*&\s*CodexOAuthManager\s*,\s*\w+\s*:\s*&\s*str\s*,?\s*\)\s*->\s*Result\s*<\s*SubscriptionQuota\s*,\s*String\s*>\s*\{', codes[COMMAND])
    # A named function with an unsupported signature cannot be hidden beside a
    # second compatible-looking one. Comments and strings have been masked.
    _one(r'\bfn\s+query_codex_oauth_quota_for\s*\(', codes[COMMAND])
    visibility = signature.group('visibility')
    if visibility is not None and not re.fullmatch(r'pub(?:\s*\(\s*crate\s*\))?\s+', visibility):
        raise PatchError('interface_changed')
    if visibility is None:
        offset = signature.start()
        after[COMMAND] = (texts[COMMAND][:offset] + 'pub(crate) ' + texts[COMMAND][offset:]).encode()
    declaration = list(re.finditer(r'\b(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+vibecue_quota\s*;', codes[SERVICES]))
    if len(declaration) > 1:raise PatchError('module_conflict')
    if not declaration:
        anchor = _one(r'\bpub\s+mod\s+subscription\s*;', codes[SERVICES])
        after[SERVICES] = (texts[SERVICES][:anchor.end()] + '\npub(crate) mod vibecue_quota;' + texts[SERVICES][anchor.end():]).encode()
    elif not re.fullmatch(r'pub\s*\(\s*crate\s*\)\s+mod\s+vibecue_quota\s*;', declaration[0].group()):
        raise PatchError('module_conflict')
    setup = _one(r'\.\s*setup\s*\(\s*(?:move\s+)?\|\s*(?P<app>\w+)\s*\|\s*\{', codes[LIB], 'setup_changed')
    setup_body = _body(codes[LIB], setup, 'setup_changed')
    # Locate the manager already held by AppState; never create a new manager.
    binding = _one(r'let\s+(?P<manager>\w+)\s*=\s*(?P<app>\w+)\s*\.\s*state\s*::\s*<\s*AppState\s*>\s*\(\s*\)\s*\.\s*codex_oauth_manager\s*\.\s*clone\s*\(\s*\)\s*;', setup_body, 'setup_changed')
    app, manager = binding.group('app'), binding.group('manager')
    if app != setup.group('app'):raise PatchError('setup_changed')
    manage_pattern = (r'\b' + re.escape(app) + r'\s*\.\s*manage\s*\(\s*CodexOAuthState\s*\(\s*' + re.escape(manager) +
                      r'\s*(?:\.\s*clone\s*\(\s*\))?\s*\)\s*\)\s*;')
    manage = _one(manage_pattern, codes[LIB], 'setup_changed')
    if not re.fullmatch(r'\s*', codes[LIB][setup.end() + binding.end():manage.start()]):
        # setup.end() follows '{'; binding offsets are relative to its body.
        raise PatchError('setup_changed')
    calls = list(re.finditer(r'\bservices\s*::\s*vibecue_quota\s*::\s*start\s*\(', codes[LIB]))
    if len(calls) > 1:raise PatchError('module_conflict')
    if calls:
        call = _one(r'\bservices\s*::\s*vibecue_quota\s*::\s*start\s*\(\s*' + re.escape(manager) + r'\s*\)\s*;', codes[LIB], 'module_conflict')
        if call.start() < manage.end() or codes[LIB][manage.end():call.start()].strip():
            raise PatchError('module_conflict')
    else:
        line_start = texts[LIB].rfind('\n', 0, manage.start()) + 1
        indent = texts[LIB][line_start:manage.start()]
        if indent.strip():raise PatchError('setup_changed')
        replacement = f'{app}.manage(CodexOAuthState({manager}.clone()));\n{indent}services::vibecue_quota::start({manager});'
        after[LIB] = (texts[LIB][:manage.start()] + replacement + texts[LIB][manage.end():]).encode()
    after[MODULE] = template
    changes = tuple(FileChange(name, originals[name], value) for name, value in after.items() if value != originals[name])
    return PatchPlan(root, tuple(originals.items()), changes, _digest(template))


def _atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.vibecue-patch-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:os.unlink(temporary)
        except FileNotFoundError:pass


def apply_patch_plan(plan):
    """Apply a validated plan, refusing source drift and rolling back failures."""
    for name, before in plan.originals:
        if _read(plan.source_dir, name, optional=before is None) != before:
            raise PatchError('source_changed')
    completed = []
    try:
        for change in plan.changes:
            _atomic_write(plan.source_dir / change.path, change.after)
            completed.append(change)
    except OSError:
        for change in reversed(completed):
            path = plan.source_dir / change.path
            if change.before is None:path.unlink(missing_ok=True)
            else:_atomic_write(path, change.before)
        raise PatchError('write_failed') from None
    return plan.manifest()


def adapt_source(source_dir, module_template=None):
    return apply_patch_plan(prepare_patch(source_dir, module_template))
