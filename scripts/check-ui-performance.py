#!/usr/bin/env python3
"""Bounded, isolated native performance contract; never scans user history."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
app = root / 'build/VibeCoding Go.app'
info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
version = tuple(int(value) for value in info['CFBundleShortVersionString'].split('.'))
if info.get('CFBundleIdentifier') != 'local.vibecue' or version < (0, 34, 1):
    raise SystemExit('Build VibeCoding Go 0.34.1 or newer before running this check.')
with tempfile.TemporaryDirectory(prefix='vcgo-ui-contract-') as home:
    env = os.environ.copy()
    env['VIBECUE_HOME'] = home
    run = subprocess.run([str(app / 'Contents/MacOS/VCGo'), '--ui-performance-measure'],
                         env=env, capture_output=True, text=True, timeout=30)
    result = json.loads(run.stdout)
    output = root / 'build/vcgo-ui-performance.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    failed = [key for key, value in result['checks'].items() if value is not True]
    watcher = result.get('watcher_measurement', {})
    if watcher.get('exact_output_checksum_equal') is not True:
        failed.append('watcher_measurement_exact_output_checksum')
    for key in ('snapshot_exact_features_equal', 'snapshot_signature_reencoding_eliminated', 'snapshot_encoded_bytes_reduced'):
        if watcher.get(key) is not True:
            failed.append('watcher_measurement_' + key)
    print(json.dumps({'failed': failed, 'measurement': result['measurement'], 'watcher_measurement': watcher, 'report': str(output)}, ensure_ascii=False))
    pages = subprocess.run([str(app / 'Contents/MacOS/VCGo'), '--resident-pages-self-test'],
                           env=env, capture_output=True, text=True, timeout=30)
    page_checks = json.loads(pages.stdout)
    page_output = root / 'build/vcgo-resident-pages-performance.json'
    page_output.write_text(json.dumps(page_checks, ensure_ascii=False, indent=2) + '\n')
    page_failed = [key for key, value in page_checks.items() if value is not True]
    print(json.dumps({'resident_page_checks': len(page_checks), 'failed': page_failed,
                      'report': str(page_output)}, ensure_ascii=False))
    preview = subprocess.run([str(app / 'Contents/MacOS/VCGo'), '--task-preview-self-test'],
                             env=env, capture_output=True, text=True, timeout=30)
    preview_checks = json.loads(preview.stdout)
    preview_output = root / 'build/vcgo-task-preview-validation.json'
    preview_output.write_text(json.dumps(preview_checks, ensure_ascii=False, indent=2) + '\n')
    preview_failed = [key for key, value in preview_checks.items() if value is not True]
    print(json.dumps({'task_preview_checks': len(preview_checks), 'failed': preview_failed,
                      'report': str(preview_output)}, ensure_ascii=False))
    batch = subprocess.run([str(app / 'Contents/MacOS/VCGo'), '--codex-batch-self-test'],
                           env=env, capture_output=True, text=True, timeout=30)
    batch_checks = json.loads(batch.stdout)
    batch_output = root / 'build/vcgo-codex-batch-validation.json'
    batch_output.write_text(json.dumps(batch_checks, ensure_ascii=False, indent=2) + '\n')
    batch_failed = [key for key, value in batch_checks.items() if value is not True]
    print(json.dumps({'codex_batch_checks': len(batch_checks), 'failed': batch_failed,
                      'report': str(batch_output)}, ensure_ascii=False))
    raise SystemExit(run.returncode or pages.returncode or preview.returncode or batch.returncode or bool(failed) or bool(page_failed) or bool(preview_failed) or bool(batch_failed))
