#!/usr/bin/env python3
"""Reproduce credited PCM clips from locally downloaded, hash-checked sources.

Download the manifest's download_url into SOURCE_DIR using that URL's basename.
This utility makes no network requests. FFmpeg is needed only to rebuild assets,
never to run the packaged app. Outputs go to an explicitly selected directory.
"""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse
import wave


def render(entry, source, output):
    if hashlib.sha256(source.read_bytes()).hexdigest() != entry['original_sha256']:
        raise ValueError('Original source checksum differs: ' + entry['id'])
    rate = entry['processing']['sample_rate']
    duration = entry['duration_seconds']
    raw = subprocess.check_output([
        'ffmpeg', '-v', 'error', '-ss', str(entry['clip_start_seconds']),
        '-i', str(source), '-t', str(duration), '-ac', '1', '-ar', str(rate),
        '-f', 'f32le', '-'])
    values = array.array('f'); values.frombytes(raw)
    if sys.byteorder != 'little':
        values.byteswap()
    count = round(duration * rate)
    if len(values) < count:
        raise ValueError('Source ends before requested clip: ' + entry['id'])
    values = values[:count]
    fade_in = entry['processing']['fade_in_seconds'] * rate
    fade_out = entry['processing']['fade_out_seconds'] * rate
    for index in range(count):
        values[index] *= min(1, index / fade_in, (count - 1 - index) / fade_out)
    # Recompute the documented normalization from the faded source. The
    # rounded gain_db in the manifest is descriptive, not a precision loss.
    rms = math.sqrt(sum(value * value for value in values) / count)
    peak = max(abs(value) for value in values)
    gain = min(.13 / rms, .65 / peak)
    pcm = array.array('h', (round(max(-.98, min(.98, value * gain)) * 32767) for value in values))
    if sys.byteorder != 'little':
        pcm.byteswap()
    with wave.open(str(output), 'wb') as handle:
        handle.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        handle.writeframes(pcm.tobytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    if digest != entry['sha256']:
        raise ValueError('Rendered clip checksum differs; check FFmpeg version: ' + entry['id'])
    return {'id': entry['id'], 'sha256': digest, 'reproduced': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--ids', help='Comma-separated IDs; omit to reproduce all recordings')
    args = parser.parse_args()
    manifest = Path(__file__).resolve().parents[1] / 'assets/Sounds/manifest.json'
    entries = json.loads(manifest.read_text())['recordings']
    selected = set(args.ids.split(',')) if args.ids else {e['id'] for e in entries}
    missing = selected - {e['id'] for e in entries}
    if missing:
        parser.error('Unknown IDs: ' + ', '.join(sorted(missing)))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry['id'] not in selected:
            continue
        filename = Path(entry['file']).name
        if filename != entry['file']:
            raise ValueError('Unsafe clip filename')
        source = args.source_dir / Path(urlparse(entry['download_url']).path).name
        print(json.dumps(render(entry, source, args.output_dir / filename), ensure_ascii=False))


if __name__ == '__main__':
    main()
