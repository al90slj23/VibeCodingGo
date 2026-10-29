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
if info.get('CFBundleIdentifier') != 'local.vibecue' or version < (0, 29, 1):
    raise SystemExit('Build VibeCoding Go 0.29.1 or newer before running this check.')
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
    raise SystemExit(run.returncode or bool(failed))
