"""Capability summaries from VibeCue's own transparent stdio bridges."""
from __future__ import annotations

import json
import os
import re
import time

from ..events import vibecue_directory


def approval_status(home=None, now=None):
    now = time.time() if now is None else now
    folder = vibecue_directory(home) / 'bridges'
    sources = []
    try:
        files = sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:64]
    except OSError:
        files = []
    for path in files:
        try:
            data = json.loads(path.read_text())
            pid = int(data['pid'])
            stamp = float(data.get('last_seen', 0))
            if (pid <= 1 or data.get('transport') != 'stdio' or
                    data.get('connected') is not True or not -2 <= now - stamp <= 15):
                continue
            os.kill(pid, 0)
            data['pending_human_requests'] = max(0, min(512, int(data.get('pending_human_requests', 0))))
            pending = data.get('pending_thread_ids', [])
            data['pending_thread_ids'] = [tid for tid in pending[:512]
                                          if isinstance(tid, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', tid)] if isinstance(pending, list) else []
            sources.append(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {'connected': bool(sources), 'status': 'connected' if sources else 'disconnected',
            'source': 'codex-client-approval-stream', 'live_sources': len(sources),
            'pending_count': sum(s['pending_human_requests'] for s in sources),
            'pending_thread_ids': sorted({tid for source in sources for tid in source['pending_thread_ids']}),
            'last_seen': max((float(s['last_seen']) for s in sources), default=0)}
