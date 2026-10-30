#!/usr/bin/env python3
"""Finite read-only reminder sampler; never triggers an alert or reads messages.

Run while a user opens a preview. CPU deltas come from cumulative process CPU
seconds, not ps's lifetime/decaying percent estimate. No full process arguments,
transcripts, proxy details, IP addresses or account values enter the report.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import time


def cpu_seconds(value):
    days, value = value.split('-', 1) if '-' in value else ('0', value)
    parts = value.split(':')
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return int(days) * 86400 + total


def status(home):
    try:
        data = json.loads((home / 'app-status.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def processes(pids):
    if not pids:
        return {}
    run = subprocess.run(['/bin/ps', '-p', ','.join(str(p) for p in pids),
                          '-o', 'pid=,time=,rss='], capture_output=True,
                         text=True, timeout=2)
    found = {}
    for line in run.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            found[int(fields[0])] = {'cpu_seconds': cpu_seconds(fields[1]),
                                     'rss_kib': int(fields[2])}
        except ValueError:
            pass
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=20)
    parser.add_argument('--interval', type=float, default=.5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    duration = max(2, min(120, args.duration))
    interval = max(.25, min(2, args.interval))
    home = Path(os.environ.get('VIBECUE_HOME', str(Path.home() / 'Library/Application Support/VibeCue')))
    ws = subprocess.run(['/usr/bin/pgrep', '-x', 'WindowServer'], capture_output=True,
                        text=True, timeout=2)
    ws_pid = int(ws.stdout.strip()) if ws.stdout.strip().isdigit() else None
    rows, previous = [], {}
    started = time.monotonic()
    next_at = started
    while time.monotonic() - started <= duration:
        data = status(home)
        app_pid = data.get('pid')
        identities = {'app': app_pid if type(app_pid) is int and app_pid > 0 else None,
                      'window_server': ws_pid}
        now = time.monotonic()
        stats = processes([pid for pid in identities.values() if pid])
        row = {'elapsed_seconds': round(now - started, 3),
               'edge_count': data.get('edge_count', 0),
               'banner_visible': data.get('banner_visible', False),
               'effect': data.get('current_effect', ''),
               'preview': data.get('current_is_preview', False),
               'effect_frame_rate': data.get('effect_frame_rate', 0),
               'effect_render_ms': data.get('effect_render_ms', []),
               'effect_timing_scope': data.get('effect_render_timing_scope', 'cpu-draw'),
               'effect_backends': data.get('effect_backends', []),
               'effect_submission_ms': data.get('effect_submission_ms', []),
               'effect_coalesced_frames': data.get('effect_coalesced_frames', []),
               'effect_rendered_frames': data.get('effect_rendered_frames', []),
               'sound_is_playing': data.get('sound_is_playing', False),
               'queued_reminders': data.get('queued', 0),
               'status_age_seconds': round(max(0, time.time() - data.get('updated_at', 0)), 3)}
        for name, pid in identities.items():
            current = stats.get(pid)
            if current is None:
                continue
            measurement = {'rss_kib': current['rss_kib']}
            if pid in previous:
                old_t, old_cpu = previous[pid]
                if now > old_t and current['cpu_seconds'] >= old_cpu:
                    measurement['cpu_percent'] = round(100 * (current['cpu_seconds'] - old_cpu) / (now - old_t), 2)
            previous[pid] = (now, current['cpu_seconds'])
            row[name] = measurement
        rows.append(row)
        next_at += interval
        time.sleep(max(0, min(interval, next_at - time.monotonic())))
    phases = {}
    for phase in ('no_overlay', 'overlay'):
        selected = [r for r in rows if bool(r['edge_count']) == (phase == 'overlay')]
        summary = {'samples': len(selected)}
        for name in ('app', 'window_server'):
            values = [r[name]['cpu_percent'] for r in selected if name in r and 'cpu_percent' in r[name]]
            if values:
                summary[name + '_cpu_median'] = round(statistics.median(values), 2)
                summary[name + '_cpu_peak'] = max(values)
        phases[phase] = summary
    result = {'duration_seconds': duration, 'interval_seconds': interval,
              'scope': 'process CPU deltas and reminder counters only; not causal whole-system attribution',
              'phases': phases, 'samples': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(args.output), 'phases': phases}, ensure_ascii=False))


if __name__ == '__main__':
    main()
