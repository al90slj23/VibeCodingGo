"""Stateless conditional dashboard pages; numeric truth stays in one WAL read.

Only an exact revision match may omit overview fields. There is no stored
partial response, time-based freshness allowance, or rounded rolling bound.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time

PROTOCOL = 1


def revision(cache, *, selection, money, metadata, now, period, start, exclusive):
    if cache.context is None:
        return None
    try:
        zone = Path('/etc/localtime').stat()
        zone_identity = (zone.st_dev, zone.st_ino, zone.st_size, zone.st_mtime_ns, zone.st_ctime_ns)
    except OSError:
        # Without a named TZ or system timezone identity we cannot prove that
        # historical calendar labels have the same local-time interpretation.
        if not os.environ.get('TZ'):
            return None
        zone_identity = None
    timezone_identity = (os.environ.get('TZ'), time.tzname, time.timezone,
                         time.altzone, time.daylight, zone_identity)
    # Hourly ranges acquire an empty new trend bucket even with no new usage.
    bucket = None
    if period in ('today', 'yesterday', '1d'):
        last_time = min(now, exclusive - .000001) if exclusive is not None else now
        first_bucket = int(datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0).timestamp())
        bucket = int((last_time - first_bucket) // 3600)
    try:
        money_identity = dict(money)
        # A not-yet-cached public source schedules its first refresh at now.
        # Only that already-due scheduler deadline is semantically identical;
        # the partial response still returns its fresh exact metadata.
        if money_identity.get('pricing_refresh_due') is True:
            money_identity['pricing_next_refresh_at'] = 'already_due'
        payload = (PROTOCOL, cache.context, selection, start, exclusive,
                   timezone_identity, bucket, money_identity, metadata)
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()
    except (ValueError, TypeError, OverflowError):
        return None


def matches(expected, current):
    return (isinstance(expected, str) and len(expected) == 64 and
            all(ch in '0123456789abcdef' for ch in expected) and
            current is not None and expected == current)
