import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('engineering_install', Path(__file__).resolve().parents[1] / 'scripts/install-engineering-tools.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def tar_bytes(name, *, member=None, link=False, duplicate=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        item = tarfile.TarInfo(member or ('package/bin/' + name))
        if link:
            item.type = tarfile.SYMTYPE; item.linkname = '/usr/bin/true'
            archive.addfile(item)
        else:
            data = ('#!/bin/sh\nprintf "' + name + ' 1.2.3\\n"\n').encode()
            item.size = len(data); item.mode = 0o755
            archive.addfile(item, io.BytesIO(data))
            if duplicate:
                other = tarfile.TarInfo('other/' + name); other.size = len(data)
                archive.addfile(other, io.BytesIO(data))
    return output.getvalue()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.archives = {name: tar_bytes(name) for name in ('mise', 'hurl', 'lnav')}
        self.tools = {name: dict(version='1.2.3', repo='fixture/' + name, tag='v1.2.3',
                                asset=name + '.tar.gz', size=len(data),
                                sha256=hashlib.sha256(data).hexdigest(), format='tar',
                                version_flag='--version', license='fixture')
                      for name, data in self.archives.items()}
        self.patcher = patch.object(installer, 'TOOLS', self.tools)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop(); self.temp.cleanup()

    def download(self, spec, path):
        name = spec['asset'].split('.')[0]
        path.write_bytes(self.archives[name])

    def install(self, **kw):
        return installer.install(self.home, downloader=self.download, **kw)

    def test_pinned_urls_sizes_and_hashes_are_fixed_official_sources(self):
        self.patcher.stop()
        try:
            self.assertEqual(set(installer.TOOLS), {'mise', 'hurl', 'lnav'})
            for name, spec in installer.TOOLS.items():
                self.assertRegex(spec['sha256'], r'^[a-f0-9]{64}$')
                self.assertGreater(spec['size'], 1000000)
                self.assertTrue(installer.source_url(spec).startswith('https://github.com/' + spec['repo'] + '/releases/download/'))
        finally:
            self.patcher.start()

    def test_explicit_install_verifies_versions_and_publishes_three_tools(self):
        result = self.install()
        self.assertEqual(result['status'], 'installed')
        root = self.home / 'engineering-tools'
        for name in self.tools:
            entry = result['tools'][name]
            self.assertTrue((root / 'bin' / name).is_symlink())
            self.assertTrue(installer.verified_entry(root, name, self.tools[name], entry, linked=True))
            self.assertIn('1.2.3', entry['version_text'])
            self.assertEqual((root / entry['binary']).stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / 'manifest.json').stat().st_mode & 0o777, 0o600)

    def test_idempotent_reinstall_has_no_download_or_process(self):
        first = self.install()
        with patch.object(installer, 'run_bounded', side_effect=AssertionError('spawned')):
            second = installer.install(self.home, downloader=lambda *_: self.fail('downloaded'))
            checked = installer.install(self.home, verify_only=True)
        self.assertEqual(first['tools'], second['tools'])
        self.assertEqual(checked['status'], 'verified')

    def test_bad_archive_never_executes_and_preserves_existing_publication(self):
        first = self.install()
        root = self.home / 'engineering-tools'
        previous = (root / 'manifest.json').read_bytes()
        # Simulate an explicit new release pin, with an untrusted download.
        self.tools['hurl']['sha256'] = 'a' * 64
        with patch.object(installer, 'verify_version', side_effect=AssertionError('executed unverified archive')):
            with self.assertRaisesRegex(installer.InstallError, '^hurl：'):
                installer.install(self.home, downloader=self.download, version_check=lambda *_: self.fail('executed'))
        self.assertEqual(previous, (root / 'manifest.json').read_bytes())
        self.assertEqual(os.readlink(root / 'bin/hurl'), '../' + first['tools']['hurl']['binary'])

    def test_tampered_binary_is_detected_and_repaired_with_new_immutable_version(self):
        first = self.install(); root = self.home / 'engineering-tools'
        bad = root / first['tools']['lnav']['binary']
        bad.write_bytes(b'broken')
        with self.assertRaises(installer.InstallError):
            installer.install(self.home, verify_only=True)
        second = self.install()
        self.assertNotEqual(first['tools']['lnav']['binary'], second['tools']['lnav']['binary'])
        self.assertEqual(bad.read_bytes(), b'broken')
        self.assertEqual(installer.install(self.home, verify_only=True)['status'], 'verified')

    def test_missing_link_is_repaired_offline_without_changing_version(self):
        first = self.install(); root = self.home / 'engineering-tools'
        (root / 'bin/hurl').unlink()
        with self.assertRaises(installer.InstallError):
            installer.install(self.home, verify_only=True)
        second = installer.install(self.home, downloader=lambda *_: self.fail('downloaded'))
        self.assertEqual(first['tools'], second['tools'])
        self.assertTrue((root / 'bin/hurl').is_symlink())

    def test_user_regular_file_is_not_overwritten(self):
        root = self.home / 'engineering-tools'; (root / 'bin').mkdir(parents=True)
        target = root / 'bin/hurl'; target.write_text('user content')
        with self.assertRaises(installer.InstallError): self.install()
        self.assertEqual(target.read_text(), 'user content')
        self.assertFalse((root / 'manifest.json').exists())
        self.assertFalse((root / 'bin/mise').exists())

    def test_failed_manifest_publish_rolls_back_links(self):
        first = self.install(); root = self.home / 'engineering-tools'
        previous = (root / 'manifest.json').read_bytes()
        self.tools['lnav']['version'] = 'new-pin'
        with patch.object(installer, 'atomic_json', side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):
                installer.install(self.home, downloader=self.download, version_check=lambda *_: 'fixture')
        self.assertEqual(previous, (root / 'manifest.json').read_bytes())
        for name in self.tools:
            self.assertEqual(os.readlink(root / 'bin' / name), '../' + first['tools'][name]['binary'])

    def test_no_archive_traversal_or_duplicate_or_link_execution(self):
        for data in (tar_bytes('hurl', member='../hurl'), tar_bytes('hurl', link=True),
                     tar_bytes('hurl', duplicate=True)):
            path = self.home / 'bad.tar.gz'; path.write_bytes(data)
            with self.assertRaises(installer.InstallError):
                installer.extract_binary(path, 'hurl', {'format':'tar'}, self.home / 'out')
            self.assertFalse((self.home / 'out').exists())

    def test_archive_root_directory_named_tool_is_not_a_binary_candidate(self):
        archive = self.home / 'mise.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            directory = tarfile.TarInfo('mise/'); directory.type = tarfile.DIRTYPE
            bundle.addfile(directory)
            directory = tarfile.TarInfo('mise/bin/'); directory.type = tarfile.DIRTYPE
            bundle.addfile(directory)
            executable = tarfile.TarInfo('mise/bin/mise'); executable.size = 6
            bundle.addfile(executable, io.BytesIO(b'binary'))
        output = self.home / 'mise-executable'
        installer.extract_binary(archive, 'mise', {'format': 'tar'}, output)
        self.assertEqual(output.read_bytes(), b'binary')
        archive = self.home / 'lnav.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('lnav/', b'')
            bundle.writestr('lnav/bin/lnav', b'zip-binary')
        output = self.home / 'lnav-executable'
        installer.extract_binary(archive, 'lnav', {'format': 'zip'}, output)
        self.assertEqual(output.read_bytes(), b'zip-binary')

    def test_zip_regular_binary_only_and_link_rejected(self):
        archive = self.home / 'tool.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('package/lnav', b'fixture-binary')
            bundle.writestr('package/readme', b'not extracted')
        target = self.home / 'out'
        installer.extract_binary(archive, 'lnav', {'format':'zip'}, target)
        self.assertEqual(target.read_bytes(), b'fixture-binary')
        self.assertFalse((self.home / 'package').exists())
        target.unlink()
        with zipfile.ZipFile(archive, 'w') as bundle:
            info = zipfile.ZipInfo('lnav'); info.external_attr = 0o120777 << 16
            bundle.writestr(info, '/usr/bin/true')
        with self.assertRaises(installer.InstallError):
            installer.extract_binary(archive, 'lnav', {'format':'zip'}, target)

    def test_binary_size_cap_applies_before_reading(self):
        with self.assertRaises(installer.InstallError):
            installer.copy_binary(io.BytesIO(b'x'), self.home / 'too-big', installer.MAX_BINARY + 1)
        self.assertFalse((self.home / 'too-big').exists())

    def test_install_directories_and_manifest_cannot_be_symlinks(self):
        root = self.home / 'engineering-tools'; root.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(installer.InstallError): self.install()
        root.unlink(); root.mkdir(); (root / 'manifest.json').symlink_to(self.home / 'outside')
        with self.assertRaises(installer.InstallError): self.install()

    def test_version_check_is_bounded_isolated_no_account_keys(self):
        binary = self.home / 'hurl'; binary.write_text('fixture')
        with patch.dict(os.environ, {'OPENAI_API_KEY':'private', 'HTTP_PROXY':'http://proxy'}):
            with patch.object(installer, 'run_bounded', return_value=b'hurl 1.2.3\n') as runner:
                installer.verify_version(binary, 'hurl', self.tools['hurl'])
        call = runner.call_args
        self.assertEqual(call.kwargs['timeout'], 8)
        self.assertNotIn('OPENAI_API_KEY', call.kwargs['env'])
        self.assertNotIn('HTTP_PROXY', call.kwargs['env'])
        self.assertNotEqual(call.kwargs['env']['HOME'], str(Path.home()))
        self.assertEqual(call.kwargs['env']['MISE_NO_CONFIG'], '1')
        with patch.object(installer, 'run_bounded', return_value=b'hurl 11.2.3\n'):
            with self.assertRaises(installer.InstallError):
                installer.verify_version(binary, 'hurl', self.tools['hurl'])

    def test_output_and_runtime_deadlines_are_bounded(self):
        with self.assertRaises(installer.InstallError):
            installer.run_bounded(['/usr/bin/yes'], timeout=1, env={}, cwd='/', limit=1024)
        with self.assertRaises(installer.InstallError):
            installer.run_bounded(['/bin/sleep', '5'], timeout=.05, env={}, cwd='/')

    def test_verify_only_does_not_create_directory(self):
        with self.assertRaises(installer.InstallError):
            installer.install(self.home, verify_only=True)
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == '__main__': unittest.main()
