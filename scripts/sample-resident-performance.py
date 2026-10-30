#!/usr/bin/env python3
"""Finite resident-only sampler. No requests, alerts or message reads are made."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import time


def cpu_seconds(value):
    days, value = value.split('-', 1) if '-' in value else ('0', value)
    total = 0.
    for part in value.split(':'):
        total = total * 60 + float(part)
    return int(days) * 86400 + total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=30)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    duration = max(2, min(120, args.duration))
    home = Path(os.environ.get('VIBECUE_HOME', str(Path.home() / 'Library/Application Support/VibeCue')))
    names = ('app-status.json', 'watcher-state.json', 'model-settings-targets.json', 'task-recovery.json')
    previous_processes, stamps, changed, rows = {}, {}, dict.fromkeys(names, 0), []
    started = time.monotonic()
    while True:
        now = time.monotonic()
        try:
            status = json.loads((home / 'app-status.json').read_text())
        except (OSError, ValueError):
            status = {}
        roles = {role: pid for role, pid in (('native_app', status.get('pid')), ('vibecue.cli.watch', status.get('watcher_child_pid')))
                 if type(pid) is int and pid > 0}
        processes = {}
        if roles:
            run = subprocess.run(['/bin/ps', '-p', ','.join(str(p) for p in roles.values()),
                                  '-o', 'pid=,time=,rss='], capture_output=True, text=True, timeout=2)
            for line in run.stdout.splitlines():
                fields = line.split()
                if len(fields) != 3:
                    continue
                try:
                    processes[int(fields[0])] = (cpu_seconds(fields[1]), int(fields[2]))
                except ValueError:
                    continue
        perf = status.get('performance', {})
        row = {'elapsed_seconds': round(now - started, 3), 'edge_count': status.get('edge_count', 0),
               'main_panel_visible': status.get('panel_visible', False),
               'banner_visible': status.get('banner_visible', False),
               'running_tasks': status.get('running_task_count'),
               'scheduler': perf.get('scheduler', {}), 'collectors': perf.get('collectors', {}),
               'inbox_signals': perf.get('inbox_signals', {}), 'usage': perf.get('usage', {}),
               'network': perf.get('network', {}), 'processes': {}}
        for role, pid in roles.items():
            if pid not in processes:
                continue
            cpu, rss = processes[pid]
            item = {'pid': pid, 'rss_kib': rss}
            if pid in previous_processes:
                before_time, before_cpu = previous_processes[pid]
                if now > before_time and cpu >= before_cpu:
                    item['cpu_percent'] = round(100 * (cpu - before_cpu) / (now - before_time), 2)
            previous_processes[pid] = (now, cpu)
            row['processes'][role] = item
        sizes = {}
        for name in names:
            try:
                stat = (home / name).stat()
                stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if name in stamps and stamp != stamps[name]:
                    changed[name] += 1
                stamps[name] = stamp; sizes[name] = stat.st_size
            except OSError:
                continue
        row['file_bytes'] = sizes; rows.append(row)
        if now - started >= duration:
            break
        time.sleep(min(2, max(0, started + duration - time.monotonic())))
    summaries = {}
    for role in ('native_app', 'vibecue.cli.watch'):
        points = [r['processes'][role] for r in rows if role in r['processes']]
        values = [p['cpu_percent'] for p in points if 'cpu_percent' in p]
        summaries[role] = {'samples': len(points), 'cpu_median': statistics.median(values) if values else None,
                           'cpu_peak': max(values) if values else None,
                           'rss_peak_kib': max((p['rss_kib'] for p in points), default=0)}
    non_alert_values = [
        b['processes']['native_app']['cpu_percent']
        for a, b in zip(rows, rows[1:])
        if a.get('edge_count') == 0 and b.get('edge_count') == 0
        and not a.get('banner_visible') and not b.get('banner_visible')
        and 'cpu_percent' in b['processes'].get('native_app', {})
    ]
    non_alert = {'samples': len(non_alert_values),
                 'cpu_median': statistics.median(non_alert_values) if non_alert_values else None,
                 'cpu_peak': max(non_alert_values) if non_alert_values else None,
                 'scope': 'interval endpoints without overlays; short notifications between samples remain possible'}
    result = {'native_non_alert_intervals': non_alert, 'duration_seconds' : round(time.monotonic() - started, 2),
              'scope': 'resident app and watcher only; short-lived workers and whole-system effects are separate',
              'process_summary': summaries, 'file_changed_intervals': changed, 'samples': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'native_non_alert_intervals': non_alert, 'process_summary': summaries, 'file_changed_intervals': changed, 'report': str(args.output)}))


if __name__ == '__main__':
    main()
