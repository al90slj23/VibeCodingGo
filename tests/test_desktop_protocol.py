import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from vibecue import desktop_protocol as protocol


CONTRACT = {'thread-stream-state-changed': 11, 'thread-owner-discovery': 1,
            'thread-stream-following-changed': 1, 'thread-follower-start-turn': 2,
            'thread-follower-read-model-settings': 1}


def script(table=None, variable='Xy'):
    return ('var ' + variable + '=' + json.dumps(CONTRACT if table is None else table,
                                                separators=(',', ':')) + ';function unused(){}').encode()


def archive(bundle, files, extras=None):
    root, payload = {'files': {}}, bytearray()
    for name, source in files.items():
        node = root
        parts = name.split('/')
        for part in parts[:-1]:
            node = node['files'].setdefault(part, {'files': {}})
        node['files'][parts[-1]] = {'size': len(source), 'offset': str(len(payload))}
        payload.extend(source)
    if extras:
        extras(root)
    index = json.dumps(root, separators=(',', ':')).encode()
    padding = (-len(index)) % 4
    size = len(index) + padding + 8
    path = bundle / 'Contents/Resources/app.asar'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack('<4I', 4, size, size - 4, len(index)) + index + b'\0' * padding + payload)
    return path


class DesktopProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name) / 'Codex.app'
        with protocol._CACHE_LOCK:
            protocol._CACHE.clear()

    def read(self):
        return protocol.read_protocol_contract(self.bundle)

    def test_hashed_script_is_discovered_without_a_build_allowlist(self):
        archive(self.bundle, {'.vite/build/src-newHash.js': script()})
        # No Info.plist/version is read: the peer's identity is the caller's job.
        self.assertEqual(self.read(), CONTRACT)

    def test_finite_main_entry_fallbacks_and_extensions(self):
        for name in ['.vite/build/source.cjs', '.vite/build/next.mjs', 'build/main.js', 'dist/main/entry-xyz.js']:
            with self.subTest(name=name):
                archive(self.bundle, {name: script()})
                self.assertEqual(self.read(), CONTRACT)

    def test_unknown_method_and_changed_versions_are_reported_not_guessed(self):
        changed = dict(CONTRACT, **{'thread-future-capability': 4, 'thread-follower-start-turn': 3})
        archive(self.bundle, {'.vite/build/protocol.js': script(changed)})
        self.assertEqual(self.read(), changed)

    def test_identical_duplicate_contracts_are_allowed(self):
        archive(self.bundle, {'.vite/build/a.js': script(), '.vite/build/b.cjs': script(variable='Other')})
        self.assertEqual(self.read(), CONTRACT)

    def test_conflicting_static_contracts_are_rejected(self):
        changed = dict(CONTRACT, **{'thread-follower-start-turn': 3})
        archive(self.bundle, {'.vite/build/a.js': script(), '.vite/build/b.js': script(changed)})
        with self.assertRaisesRegex(protocol.ProtocolContractError, '冲突'):
            self.read()

    def test_extra_field_difference_between_tables_is_not_merged(self):
        archive(self.bundle, {'.vite/build/a.js': script(), '.vite/build/b.js': script(dict(CONTRACT, future=1))})
        with self.assertRaisesRegex(protocol.ProtocolContractError, '冲突'):
            self.read()

    def test_duplicate_json_method_keys_are_rejected(self):
        source = script().replace(b'"thread-owner-discovery":1', b'"thread-owner-discovery":1,"thread-owner-discovery":2')
        archive(self.bundle, {'.vite/build/a.js': source})
        with self.assertRaisesRegex(protocol.ProtocolContractError, '重复'):
            self.read()

    def test_non_integer_and_out_of_bounds_versions_are_rejected(self):
        for version in [True, None, '2', -1, protocol.MAX_METHOD_VERSION + 1, 1.5, {}]:
            with self.subTest(version=version):
                archive(self.bundle, {'.vite/build/a.js': script(dict(CONTRACT, invalid=version))})
                with self.assertRaises(protocol.ProtocolContractError):
                    self.read()

    def test_js_is_not_executed_and_dynamic_objects_are_not_inferred(self):
        for source in [b'var x={};process.exit(99);',
                       b'var x={"thread-owner-discovery":1,"thread-stream-state-changed":runCode()};',
                       b'var x={"thread-owner-discovery":1,...other,"thread-stream-state-changed":11};',
                       b'var x={}; // "thread-owner-discovery": 1']:
            archive(self.bundle, {'.vite/build/a.js': source})
            with self.assertRaises(protocol.ProtocolContractError):
                self.read()

    def test_arbitrary_payload_object_is_not_a_method_contract(self):
        source = b'call(' + script().split(b'=', 1)[1].split(b';', 1)[0] + b');'
        archive(self.bundle, {'.vite/build/a.js': source})
        with self.assertRaises(protocol.ProtocolContractError):
            self.read()

    def test_missing_empty_and_renderer_only_contracts_are_rejected(self):
        for files in [{}, {'.vite/build/a.js': b'var x={};'}, {'node_modules/anything/main.js': script()},
                      {'webview/assets/main.js': script()}, {'.vite/build/renderer/assets.js': script()}]:
            with self.subTest(files=list(files)):
                archive(self.bundle, files)
                with self.assertRaises(protocol.ProtocolContractError):
                    self.read()

    def test_malformed_header_and_oversized_header_are_rejected(self):
        path = archive(self.bundle, {'.vite/build/a.js': script()})
        for prefix in [b'', struct.pack('<4I', 3, 8, 4, 0), struct.pack('<4I', 4, 1024, 1020, 1),
                       struct.pack('<4I', 4, protocol.MAX_HEADER_BYTES + 4, protocol.MAX_HEADER_BYTES, 4)]:
            path.write_bytes(prefix)
            with self.assertRaises(protocol.ProtocolContractError):
                self.read()

    def test_invalid_json_header_is_rejected(self):
        path = archive(self.bundle, {})
        data = path.read_bytes()
        path.write_bytes(data[:16] + b'!' + data[17:])
        with self.assertRaisesRegex(protocol.ProtocolContractError, '索引'):
            self.read()

    def test_corrupt_entry_offset_does_not_read_outside_archive(self):
        def corrupt(root):
            root['files']['.vite']['files']['build']['files']['a.js']['offset'] = '999999999999'
        archive(self.bundle, {'.vite/build/a.js': script()}, extras=corrupt)
        with self.assertRaisesRegex(protocol.ProtocolContractError, '位置'):
            self.read()

    def test_unpacked_and_link_entries_are_not_followed(self):
        for update in [{'unpacked': True}, {'link': '../../../elsewhere.js'}]:
            def corrupt(root):
                root['files']['.vite']['files']['build']['files']['a.js'].update(update)
            archive(self.bundle, {'.vite/build/a.js': script()}, extras=corrupt)
            with self.assertRaises(protocol.ProtocolContractError):
                self.read()

    def test_oversize_script_is_skipped_without_blocking_small_contract(self):
        archive(self.bundle, {'.vite/build/a.js': b'x' * 1024, '.vite/build/b.js': script()})
        with patch.object(protocol, 'MAX_SCRIPT_BYTES', 512):
            self.assertEqual(self.read(), CONTRACT)

    def test_total_scan_and_script_count_are_bounded(self):
        archive(self.bundle, {'.vite/build/a.js': b'a' * 40, '.vite/build/b.js': script()})
        with patch.object(protocol, 'MAX_SCAN_BYTES', 50):
            with self.assertRaisesRegex(protocol.ProtocolContractError, '有界'):
                self.read()
        with protocol._CACHE_LOCK:
            protocol._CACHE.clear()
        with patch.object(protocol, 'MAX_SCRIPTS', 1):
            with self.assertRaisesRegex(protocol.ProtocolContractError, '有界'):
                self.read()

    def test_cache_reuses_success_and_returns_independent_copies(self):
        archive(self.bundle, {'.vite/build/a.js': script()})
        with patch.object(protocol, '_read_contract', wraps=protocol._read_contract) as reader:
            first = self.read(); first['thread-owner-discovery'] = 99
            self.assertEqual(self.read(), CONTRACT)
            self.assertEqual(reader.call_count, 1)

    def test_failed_parse_is_cached_and_update_retries_automatically(self):
        path = archive(self.bundle, {'.vite/build/a.js': b'var x={};'})
        with patch.object(protocol, '_read_contract', wraps=protocol._read_contract) as reader:
            for _ in range(2):
                with self.assertRaises(protocol.ProtocolContractError):
                    self.read()
            self.assertEqual(reader.call_count, 1)
            old = path.stat().st_mtime_ns
            archive(self.bundle, {'.vite/build/b.js': script()})
            os.utime(path, ns=(old + 1000000, old + 1000000))
            self.assertEqual(self.read(), CONTRACT)
            self.assertEqual(reader.call_count, 2)

    def test_transient_failure_expires_without_changing_archive(self):
        archive(self.bundle, {'.vite/build/a.js': script()})
        with patch.object(protocol, '_read_contract', side_effect=[protocol.ProtocolContractError('temporary access'), CONTRACT]) as reader, \
                patch.object(protocol.time, 'monotonic', side_effect=[100, 101, 131]):
            for _ in range(2):
                with self.assertRaises(protocol.ProtocolContractError): self.read()
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(self.read(), CONTRACT)
            self.assertEqual(reader.call_count, 2)

    def test_atomic_archive_replacement_invalidates_cache_even_same_mtime(self):
        path = archive(self.bundle, {'.vite/build/a.js': script()})
        self.assertEqual(self.read(), CONTRACT)
        old = path.stat()
        changed = dict(CONTRACT, **{'thread-follower-start-turn': 3})
        other = self.bundle.parent / 'Other.app'
        replacement = archive(other, {'.vite/build/a.js': script(changed)})
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(path)
        self.assertEqual(self.read(), changed)

    def test_cache_has_a_small_finite_capacity(self):
        for number in range(protocol.CACHE_ENTRIES + 3):
            bundle = self.bundle.parent / ('Codex%d.app' % number)
            archive(bundle, {'.vite/build/a.js': script()})
            self.assertEqual(protocol.read_protocol_contract(bundle), CONTRACT)
        self.assertEqual(len(protocol._CACHE), protocol.CACHE_ENTRIES)

    def test_symlink_archive_is_rejected(self):
        path = archive(self.bundle, {'.vite/build/a.js': script()})
        target = path.with_suffix('.original')
        path.rename(target); path.symlink_to(target)
        with self.assertRaises(protocol.ProtocolContractError):
            self.read()


if __name__ == '__main__':
    unittest.main()
